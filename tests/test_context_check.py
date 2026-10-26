"""The context check: did the planning context hold the files a fix changed?"""

from __future__ import annotations

from nightshift.worker.relevant import changed_files, coverage

DIFF = """diff --git a/src/app/server.py b/src/app/server.py
index 1111111..2222222 100644
--- a/src/app/server.py
+++ b/src/app/server.py
@@ -1 +1 @@
-a
+b
diff --git a/src/app/models.py b/src/app/models.py
index 1111111..2222222 100644
--- a/src/app/models.py
+++ b/src/app/models.py
@@ -1 +1 @@
-a
+b
diff --git a/src/app/limits.py b/src/app/limits.py
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/src/app/limits.py
@@ -0,0 +1 @@
+c
diff --git a/tests/test_server.py b/tests/test_server.py
index 1111111..2222222 100644
--- a/tests/test_server.py
+++ b/tests/test_server.py
@@ -1 +1 @@
-a
+b
diff --git a/README.md b/README.md
index 1111111..2222222 100644
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-a
+b
"""


def test_changed_source_files_leave_out_tests_and_docs_and_split_off_new_files() -> None:
    assert changed_files(DIFF) == (["src/app/server.py", "src/app/models.py"], ["src/app/limits.py"])


def test_a_file_in_neither_the_relevant_files_nor_the_plan_is_a_miss() -> None:
    cc = coverage(DIFF, relevant=["src/app/server.py", "src/app/cli.py"], planned=["src/app/server.py"])
    assert cc == {
        "changed": ["src/app/server.py", "src/app/models.py"],
        "relevant": ["src/app/server.py"],
        "planned": ["src/app/server.py"],
        "missed": ["src/app/models.py"],
        "new": ["src/app/limits.py"],
    }


def test_a_file_the_plan_named_is_not_a_miss_even_when_the_search_missed_it() -> None:
    cc = coverage(DIFF, relevant=[], planned=["src/app/server.py", "src/app/models.py"])
    assert cc["missed"] == [] and cc["relevant"] == []
