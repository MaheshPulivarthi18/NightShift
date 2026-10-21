from __future__ import annotations

import json

import httpx

from nightshift.github import GitHubClient
from nightshift.health.brief import briefer_input, missing_paths, tree_summary

PATHS = [
    "README.md",
    "pyproject.toml",
    "src/app/cache.py",
    "src/app/proxy.py",
    "src/cli/main.py",
    "tests/test_cache.py",
    "ui/src-tauri/src/runtime.rs",
]


def test_tree_summary_counts_folders() -> None:
    text = tree_summary(PATHS)
    assert text.splitlines()[0] == "7 files"
    assert "src/ (3 files)" in text and "  src/app/ (2)" in text
    assert "root files: README.md, pyproject.toml" in text


def test_briefer_input_trims_long_documents() -> None:
    docs = {"README.md": "r" * 10_000, "CONTRIBUTING.md": "c" * 100}
    payload = briefer_input({"docs": docs, "paths": PATHS, "description": None, "language": "Rust"})
    assert payload["documents"]["README.md"].endswith("[truncated]")
    assert payload["documents"]["CONTRIBUTING.md"] == "c" * 100


def test_missing_paths() -> None:
    text = (
        "Edit `ui/src-tauri/src/runtime.rs` (or just `runtime.rs`), look in `src/app/`, "
        "run `pytest -k cache`, then check `src/app/missing.py` and `--flag`."
    )
    assert missing_paths(text, set(PATHS)) == ["src/app/missing.py"]


async def test_fetch_repo_docs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.read())
            assert "HEAD:.github/CONTRIBUTING.md" in body["query"]
            repo = {
                "description": "d",
                "primaryLanguage": {"name": "Python"},
                "defaultBranchRef": {"name": "main", "target": {"oid": "abc123"}},
                "readme": {"text": "Hello", "byteSize": 5},
                "contributing_github": {"text": "Max 10 PRs", "byteSize": 10},
                "contributing": None,
            }
            return httpx.Response(200, json={"data": {"repository": repo}})
        assert str(request.url) == "https://api.github.com/repos/o/r/git/trees/abc123?recursive=1"
        tree = [{"path": "src", "type": "tree"}, {"path": "src/a.py", "type": "blob"}]
        return httpx.Response(200, json={"tree": tree, "truncated": False})

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    docs = await client.fetch_repo_docs("o/r")
    await client.aclose()
    assert docs["docs"] == {"README.md": "Hello", ".github/CONTRIBUTING.md": "Max 10 PRs"}
    assert docs["paths"] == ["src/a.py"] and docs["head_oid"] == "abc123"
