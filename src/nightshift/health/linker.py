"""Candidate groups of related items, found without a model.

Edges come from explicit links (closing references, cross-references, duplicate marks)
and from embedding similarity. Connected components become candidate groups, which the
Relevance Analyst then confirms, splits or rejects.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from nightshift.db import Item

Edge = tuple[int, int, str]  # (number_a, number_b, why)


def explicit_edges(items: Iterable[Item]) -> list[Edge]:
    numbers = {it.number for it in items}
    edges: list[Edge] = []
    for it in items:
        d = it.data
        for ref in d.get("cross_refs", []):
            if ref in numbers:
                edges.append((it.number, ref, "cross-reference"))
        for ref in d.get("closing_issues", []):
            if ref["number"] in numbers:
                edges.append((it.number, ref["number"], "closes"))
        if (dup := d.get("duplicate_of")) in numbers:
            edges.append((it.number, dup, "marked duplicate"))
    return edges


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def candidate_groups(open_numbers: set[int], edges: list[Edge], max_size: int) -> list[dict[str, object]]:
    """Group items that touch at least one open item. Large components are split by edge strength.

    Explicit edges are always kept; similarity edges (why starts with "similar") are dropped
    weakest-first until every group fits in `max_size`.
    """
    explicit = [e for e in edges if not e[2].startswith("similar")]
    similar = sorted(
        (e for e in edges if e[2].startswith("similar")),
        key=lambda e: float(e[2].split(":")[1]),
        reverse=True,
    )

    def build(active: list[Edge]) -> dict[int, set[int]]:
        uf = _UnionFind()
        for a, b, _ in active:
            uf.union(a, b)
        comps: dict[int, set[int]] = defaultdict(set)
        for a, b, _ in active:
            comps[uf.find(a)].update((a, b))
        return comps

    kept = list(similar)
    comps = build(explicit + kept)
    while kept and any(len(c) > max_size for c in comps.values()):
        kept.pop()  # drop the weakest similarity edge
        comps = build(explicit + kept)

    active = explicit + kept
    groups = []
    for members in comps.values():
        if not members & open_numbers or len(members) < 2:
            continue
        why = sorted({f"#{a}-#{b} {w}" for a, b, w in active if a in members and b in members})
        ordered = sorted(members)
        # A component held together only by explicit links can still be large; chunk it.
        for i in range(0, len(ordered), max_size):
            chunk = ordered[i : i + max_size]
            if len(chunk) >= 2:
                groups.append({"numbers": chunk, "evidence": why})
    return groups
