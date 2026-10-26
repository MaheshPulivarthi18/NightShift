"""Layered prompts: stable layers first, marked for the cache, cut visibly when over budget."""

from __future__ import annotations

from nightshift.prompt import Prompt, tokens
from nightshift.worker.planner import plan_prompt
from nightshift.worker.reviewer import review_prompt


def test_layers_render_in_order_and_only_the_stable_ones_are_cached() -> None:
    p = Prompt().add("task", "diff", "d", 100).add("item", "issue", "i", 100).add("repo", "rules", "r", 100)
    blocks = p.blocks()
    assert [b["text"] for b in blocks] == ["<rules>\nr\n</rules>", "<issue>\ni\n</issue>", "<diff>\nd\n</diff>"]
    assert [b.get("cache_control") for b in blocks] == [{"type": "ephemeral"}, {"type": "ephemeral"}, None]


def test_empty_sections_add_nothing_and_same_inputs_give_same_bytes() -> None:
    def build() -> Prompt:
        return Prompt().add("repo", "brief", "b", 100).add("item", "note", "  ", 100).add("task", "files", "", 100)

    assert (
        build().blocks()
        == build().blocks()
        == [{"type": "text", "text": "<brief>\nb\n</brief>", "cache_control": {"type": "ephemeral"}}]
    )


def test_a_section_over_budget_keeps_its_start_and_end_and_reports_the_cut() -> None:
    lines = [f"line {i:04d}" for i in range(2000)]
    p = Prompt().add("item", "issue", "\n".join(lines), 200).add("repo", "brief", "short", 100)
    text = p.blocks()[1]["text"]
    assert "line 0000" in text and "line 1999" in text  # the request and the thread's last word both stay
    assert "line 1000" not in text
    assert "[cut here: about" in text and "200-token budget for <issue>" in text
    assert tokens(text) < 260
    assert p.report() == {"repo": {"tokens": tokens("<brief>\nshort\n</brief>"), "cut": []},
                          "item": {"tokens": tokens(text), "cut": ["issue"]}}  # fmt: skip


def test_review_rounds_share_the_start_of_their_prompt() -> None:
    def round_(diff: str, verification: str) -> list[dict[str, object]]:
        return review_prompt(
            repo="o/r", issue="Crash on empty input", plan="Guard the empty case", rules="Small PRs",
            verification=verification, diff=diff, change_context="callers",
        ).blocks()  # fmt: skip

    first, second = round_("diff one", "1 failing"), round_("diff two", "all pass")
    assert first[:2] == second[:2]  # rules, then issue and plan: read from the cache in round two
    assert first[2] != second[2]
    assert first[0]["text"].index("<repo_rules>") >= 0 and "<untrusted_github_data>" in first[1]["text"]


def test_a_second_plan_reuses_the_repo_and_issue_layers() -> None:
    inputs = {
        "repo": "o/r", "brief": "A CLI", "architecture_map": "map", "environment": "ready", "recipe": "",
        "issue": "Crash on empty input", "relevant_files": "src/cli.py", "note": "keep it small",
    }  # fmt: skip
    first, again = plan_prompt(**inputs).blocks(), plan_prompt(**inputs, setup_failed="pip install failed").blocks()
    assert first[:2] == again[:2]
    assert "<developer_note>" in first[1]["text"] and "<commands_that_worked_before>\nnone yet" in first[0]["text"]
    assert "<setup_failed>" in again[2]["text"] and "<setup_failed>" not in first[2]["text"]
