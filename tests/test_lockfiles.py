"""A lock file's diff, read as the packages it changes."""

from __future__ import annotations

from nightshift.worker.lockfiles import changes, summaries, summarize_diff

UV = """diff --git a/uv.lock b/uv.lock
--- a/uv.lock
+++ b/uv.lock
@@ -10,7 +10,7 @@
 [[package]]
 name = "requests"
-version = "2.31.0"
+version = "2.32.3"
 source = { registry = "https://pypi.org/simple" }
 dependencies = [
     { name = "certifi" },
@@ -40,6 +40,11 @@
+[[package]]
+name = "urllib3"
+version = "2.2.1"
+source = { registry = "https://pypi.org/simple" }
+
 [[package]]
-name = "idna"
-version = "3.6"
-source = { registry = "https://pypi.org/simple" }
"""

CODE = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


def test_uv_lock() -> None:
    assert changes(UV.splitlines()) == {
        "idna": ("3.6", None), "requests": ("2.31.0", "2.32.3"), "urllib3": (None, "2.2.1"),
    }  # fmt: skip


def test_package_lock_yarn_go_sum_requirements_and_pnpm() -> None:
    npm = ['   "node_modules/@babel/core": {', '-    "version": "7.23.0",', '+    "version": "7.24.1",',
           '     "resolved": "x"']  # fmt: skip
    assert changes(npm) == {"@babel/core": ("7.23.0", "7.24.1")}
    yarn = [" lodash@^4.17.0:", '-  version "4.17.20"', '+  version "4.17.21"']
    assert changes(yarn) == {"lodash": ("4.17.20", "4.17.21")}
    gosum = ["-golang.org/x/net v0.20.0 h1:abc=", "-golang.org/x/net v0.20.0/go.mod h1:def=",
             "+golang.org/x/net v0.21.0 h1:ghi=", "+golang.org/x/net v0.21.0/go.mod h1:jkl="]  # fmt: skip
    assert changes(gosum) == {"golang.org/x/net": ("v0.20.0", "v0.21.0")}
    reqs = ["-httpx==0.27.0 \\\\", "+httpx==0.28.1 \\\\", "     --hash=sha256:abc", "+anyio[trio]==4.4.0"]
    assert changes(reqs) == {"anyio": (None, "4.4.0"), "httpx": ("0.27.0", "0.28.1")}
    pnpm = ["-  /react@18.2.0:", "+  /react@18.3.1:", "     resolution: {integrity: x}"]
    assert changes(pnpm) == {"react": ("18.2.0", "18.3.1")}


def test_the_diff_keeps_code_and_shows_locks_as_packages() -> None:
    out = summarize_diff(CODE + UV)
    assert out.startswith(CODE)  # code is untouched
    assert "(regenerated lock file: 10 changed lines, shown as the 3 packages it changes)" in out
    assert "  requests 2.31.0 → 2.32.3\n" in out and "  urllib3 2.2.1 (added)" in out and "idna 3.6 (removed)" in out
    assert "source = { registry" not in out
    assert summaries(CODE + UV) == {"uv.lock": ["idna 3.6 (removed)", "requests 2.31.0 → 2.32.3",
                                                "urllib3 2.2.1 (added)"]}  # fmt: skip
    assert summarize_diff(CODE) == CODE


def test_the_reviewer_reads_the_packages_not_the_lines() -> None:
    from nightshift.worker.reviewer import review_prompt

    text = str(review_prompt(repo="o/r", issue="i", plan="p", rules="", verification="v", diff=CODE + UV,
                             change_context=""))  # fmt: skip
    assert "requests 2.31.0 → 2.32.3" in text and "source = { registry" not in text and "+x = 2" in text
