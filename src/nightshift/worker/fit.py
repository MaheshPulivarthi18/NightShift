"""How well a repo fits you: your languages and the repos you already contribute to, from your GitHub.

Your profile is read from public data for `github_login`: the languages of your own repos (by code size) and
the repos you opened pull requests in this past year. A repo fits by how much of its code is in languages you
write, weighted by how much you write them, with a bonus where you already have PRs. Markup and notebooks say
little about what you can change, so they are left out of both sides. Kept for a few hours.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from nightshift.config import get_settings
from nightshift.github import GitHubClient, GitHubError

log = structlog.get_logger()

CACHE_SECONDS = 6 * 3600
IGNORED = {"CSS", "SCSS", "HTML", "Jupyter Notebook", "Makefile", "Dockerfile", "Shell", "Batchfile", "PowerShell",
           "Procfile", "Mako", "Jinja", "Handlebars", "Less", "Roff", "TeX"}  # fmt: skip
CONTRIBUTED_BONUS = 0.25  # you already have PRs there: you know its code and its maintainers know you
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

PROFILE = """query($login: String!) { user(login: $login) {
  repositories(first: 100, ownerAffiliations: OWNER, isFork: false, orderBy: {field: PUSHED_AT, direction: DESC}) {
    nodes { languages(first: 6, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } } } }
  contributionsCollection { pullRequestContributionsByRepository(maxRepositories: 50) {
    repository { nameWithOwner primaryLanguage { name } } contributions { totalCount } } } } }"""


def _shares(sizes: dict[str, int]) -> dict[str, float]:
    sizes = {k: v for k, v in sizes.items() if k not in IGNORED and v > 0}
    total = sum(sizes.values())
    return {k: v / total for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])} if total else {}


def profile_from(data: dict[str, Any]) -> dict[str, Any]:
    """{"languages": {name: share of your code}, "prs": {repo: PRs you opened there}} from the GraphQL answer."""
    user = data.get("user") or {}
    sizes: dict[str, int] = {}
    for r in (user.get("repositories") or {}).get("nodes") or []:
        for e in (r.get("languages") or {}).get("edges") or []:
            sizes[e["node"]["name"]] = sizes.get(e["node"]["name"], 0) + int(e["size"])
    prs: dict[str, int] = {}
    for c in ((user.get("contributionsCollection") or {}).get("pullRequestContributionsByRepository")) or []:
        name = c["repository"]["nameWithOwner"]
        prs[name.lower()] = int((c.get("contributions") or {}).get("totalCount") or 0)
    return {"languages": _shares(sizes), "prs": prs}


def score(repo_languages: dict[str, int], you: dict[str, Any], full_name: str) -> dict[str, Any]:
    """{"score": 0..1, "label", "why", "prs_here"} for one repo. A language you write most counts fully; one
    you write half as much counts half."""
    here = _shares(repo_languages)
    mine: dict[str, float] = you.get("languages") or {}
    top = max(mine.values(), default=0.0)
    overlap = sum(share * min(1.0, mine.get(lang, 0.0) / top) for lang, share in here.items()) if top else 0.0
    prs_here = (you.get("prs") or {}).get(full_name.lower(), 0)
    value = min(1.0, overlap + (CONTRIBUTED_BONUS if prs_here else 0.0))
    label = "strong" if value >= 0.6 else "some" if value >= 0.25 else "little"
    main = next(iter(here), None)
    parts = []
    if main:
        yours = mine.get(main, 0.0)
        parts.append(f"{main} is {round(here[main] * 100)}% of its code"
                     + (f"; {round(yours * 100)}% of yours" if yours else "; you do not write it"))  # fmt: skip
    if prs_here:
        parts.append(f"you opened {prs_here} PR{'s' if prs_here > 1 else ''} here this year")
    return {"score": round(value, 2), "label": label, "why": "; ".join(parts) or "no languages to compare",
            "prs_here": prs_here}  # fmt: skip


async def _repo_languages(gh: GitHubClient, names: list[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for start in range(0, len(names), 20):  # one query per 20 repos
        chunk = names[start : start + 20]
        fields = " ".join(
            f'r{i}: repository(owner: "{n.split("/")[0]}", name: "{n.split("/")[1]}") '
            "{ languages(first: 8, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } } }"
            for i, n in enumerate(chunk)
        )
        data = await gh.graphql(f"query {{ {fields} }}", {})
        for i, n in enumerate(chunk):
            edges = ((data.get(f"r{i}") or {}).get("languages") or {}).get("edges") or []
            out[n] = {e["node"]["name"]: int(e["size"]) for e in edges}
    return out


async def fit(repos: list[str]) -> dict[str, Any]:
    """Your profile and each repo's fit. Without a GitHub login there is nothing to compare: said so."""
    login = get_settings().github_login
    if not login:
        return {"login": None, "languages": {}, "repos": {}, "error": "Set NIGHTSHIFT_GITHUB_LOGIN to rank by fit."}
    key = f"{login}|{','.join(sorted(repos))}"
    if (hit := _cache.get(key)) and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    gh = GitHubClient()
    try:
        you = profile_from(await gh.graphql(PROFILE, {"login": login}))
        langs = await _repo_languages(gh, [r for r in repos if r.count("/") == 1])
    except GitHubError as e:
        return {"login": login, "languages": {}, "repos": {}, "error": f"GitHub did not answer: {e}"}
    finally:
        await gh.aclose()
    out = {
        "login": login,
        "languages": {k: round(v, 2) for k, v in list(you["languages"].items())[:5]},
        "repos": {r: score(langs.get(r, {}), you, r) for r in repos},
    }
    _cache[key] = (time.monotonic(), out)
    return out
