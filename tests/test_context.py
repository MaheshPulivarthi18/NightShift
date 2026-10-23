"""Context ladder: the sandbox scripts' pure helpers, and the host-side validation and rendering."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

from nightshift.context import archmap
from nightshift.context.change import ChangeContext, Finding, to_markdown
from nightshift.sandbox.docker import INSIDE_DIR


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"inside_{name}", INSIDE_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"inside_{name}"] = module  # dataclasses look their module up while it runs
    spec.loader.exec_module(module)
    return module


cc = _load("change_context")
si = _load("source_index")

DIFF = """\
diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -10,3 +10,4 @@ def f():
     x = 1
+    y = 2
     return x
     # end
diff --git a/old.py b/old.py
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-gone
"""


def test_parse_diff_tracks_added_lines_in_new_file_numbering() -> None:
    hunks = cc.parse_diff(DIFF)
    assert len(hunks) == 1  # the deleted file has no new-side lines
    h = hunks[0]
    assert (h.path, h.start, h.end, h.added) == ("pkg/a.py", 9, 12, [10])


def test_import_lines_are_not_uses() -> None:
    lines = ["from x import (", "    alpha,", "    beta,", ")", "beta()"]
    assert cc.is_import_line(lines, 1)
    assert not cc.is_import_line(lines, 4)
    ts = ["import {", "  ChatMessage,", "} from './types';", "const m: ChatMessage = x;"]
    assert cc.is_import_line(ts, 1)
    assert not cc.is_import_line(ts, 3)


def test_is_source_skips_tests_docs_and_vendored_code() -> None:
    assert cc.is_source("engine/laya/api/ws_router.py")
    assert cc.is_source("ui/src/lib/api/engine.ts")
    for path in ("engine/tests/test_chat.py", "ui/src/a.test.ts", "README.md", "ui/node_modules/x/index.js"):
        assert not cc.is_source(path)


def test_name_by_indent_finds_enclosing_function() -> None:
    py = ["class A:", "    async def handle(self):", "        send({'a': 1})"]
    assert cc.name_by_indent(py, 2) == "handle"
    ts = ["export async function load() {", "  return fetch('/x');", "}"]
    assert cc.name_by_indent(ts, 1) == "load"


def test_py_imports_resolve_relative_and_absolute() -> None:
    # every dotted suffix of each path, as build() maps them
    modules = {
        "engine.laya.api.chat_api": "engine/laya/api/chat_api.py",
        "laya.api.chat_api": "engine/laya/api/chat_api.py",
        "engine.laya.models.chat": "engine/laya/models/chat.py",
        "laya.models.chat": "engine/laya/models/chat.py",
    }
    text = "from ..models.chat import ChatMessage\nimport laya.api.chat_api\n"
    found = si.py_imports(text, "engine/laya/api/ws_router.py", modules)
    assert found == {"engine/laya/models/chat.py", "engine/laya/api/chat_api.py"}


def test_js_imports_resolve_aliases_and_relative_paths() -> None:
    files = {"ui/src/lib/api/engine.ts", "ui/src/lib/utils/toolLabels.ts", "ui/src/routes/+page.svelte"}
    text = "import { a } from '$lib/api/engine';\nimport { b } from '../lib/utils/toolLabels';\nimport x from 'svelte';"
    found = si.js_imports(text, "ui/src/routes/+page.svelte", files, {"$lib": "ui/src/lib"})
    assert found == {"ui/src/lib/api/engine.ts", "ui/src/lib/utils/toolLabels.ts"}


def test_rust_imports_follow_mod_and_use_crate() -> None:
    files = {"app/src/main.rs", "app/src/sidecar.rs", "app/src/net/mod.rs", "app/src/net/http.rs"}
    text = "mod sidecar;\npub mod net;\nuse crate::net::http::Client;\n"
    assert si.rust_imports(text, "app/src/main.rs", files, "app/src") == {
        "app/src/sidecar.rs",
        "app/src/net/mod.rs",
        "app/src/net/http.rs",
    }


def _map() -> archmap.ArchitectureMap:
    return archmap.ArchitectureMap(
        summary="s",
        components=[
            archmap.Component(id="api", name="API", group="Engine", path="engine/api", responsibility="r"),
            archmap.Component(id="db", name="DB", group="Engine", path="engine/db.py", responsibility="r"),
            archmap.Component(id="ghost", name="Ghost", group="Engine", path="engine/nope.py", responsibility="r"),
            archmap.Component(id="user", name="User", group="External", path=None, responsibility="r"),
        ],
        relationships=[
            archmap.Relationship(source="api", target="db", verb="writes", evidence_path="engine/api/x.py"),
            archmap.Relationship(source="api", target="ghost", verb="calls", evidence_path="engine/api/x.py"),
            archmap.Relationship(source="user", target="api", verb="calls", evidence_path="made/up.py"),
            archmap.Relationship(source="user", target="db", verb="probably reads", evidence_path=None),
        ],
    )


def test_validate_drops_invented_paths_and_unsupported_relationships() -> None:
    amap = _map()
    dropped = archmap.validate(amap, ["engine/api/x.py", "engine/db.py"])
    assert [c.id for c in amap.components] == ["api", "db", "user"]  # a directory path counts
    assert dropped == {"dropped_components": ["engine/nope.py"], "dropped_relationships": 2}
    assert [(r.source, r.target) for r in amap.relationships] == [("api", "db"), ("user", "db")]


def test_html_escapes_repo_text() -> None:
    amap = _map()
    archmap.validate(amap, ["engine/api/x.py", "engine/db.py"])
    amap.summary = "<script>alert(1)</script>"
    meta = {"commit": "abc1234def", "model": "m", "cost_usd": 0.1, "components": 3, "relationships": 2, "inferred": 1}
    page = archmap.to_html("o/r", {"meta": meta, **amap.model_dump()})
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


def test_context_pack_lists_unchanged_callers_first_and_respects_budget() -> None:
    def f(kind: str, touched: bool, score: int, line: int) -> Finding:
        return Finding(kind=kind, path="a.py", symbol="s", line=line, reason="r", touched_by_diff=touched,
                       snippet="x" * 400, score=score)  # fmt: skip

    ctx = ChangeContext(
        base="e5f1610abc",
        files_changed=["a.py"],
        findings=[f("enclosing", True, 100, 1), f("caller", False, 80, 2), f("caller", True, 80, 3)],
    )
    assert [x.line for x in ctx.unchanged_related] == [2]
    text, used = to_markdown(ctx, budget_tokens=200)
    assert text.index("NOT changed") < text.index("## Context")
    assert "budget reached" in text
    assert used <= 200
