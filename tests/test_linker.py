from __future__ import annotations

from conftest import make_item

from nightshift.health.linker import candidate_groups, explicit_edges


def test_explicit_edges_only_inside_repo_snapshot() -> None:
    a = make_item(1, kind="issue", cross_refs=[2, 999])
    b = make_item(2, closing_issues=[{"number": 1, "state": "OPEN"}])
    c = make_item(3, kind="issue", duplicate_of=1)
    edges = explicit_edges([a, b, c])
    assert (1, 2, "cross-reference") in edges
    assert (2, 1, "closes") in edges
    assert (3, 1, "marked duplicate") in edges
    assert all(999 not in e[:2] for e in edges)


def test_groups_require_an_open_member_and_two_items() -> None:
    edges = [(1, 2, "closes"), (3, 4, "similar:0.900")]
    groups = candidate_groups(open_numbers={1}, edges=edges, max_size=8)
    assert [g["numbers"] for g in groups] == [[1, 2]]


def test_weak_similarity_edges_are_dropped_to_fit_size() -> None:
    # A chain 1-2-3-4 through similarity; the weakest link (2-3) should go first.
    edges = [(1, 2, "similar:0.950"), (2, 3, "similar:0.830"), (3, 4, "similar:0.940")]
    groups = candidate_groups(open_numbers={1, 2, 3, 4}, edges=edges, max_size=2)
    assert sorted(g["numbers"] for g in groups) == [[1, 2], [3, 4]]


def test_explicit_edges_survive_and_large_components_are_chunked() -> None:
    edges = [(1, n, "closes") for n in range(2, 6)]
    groups = candidate_groups(open_numbers={1}, edges=edges, max_size=3)
    assert sum(len(g["numbers"]) for g in groups) == 5
    assert all(len(g["numbers"]) <= 3 for g in groups)
