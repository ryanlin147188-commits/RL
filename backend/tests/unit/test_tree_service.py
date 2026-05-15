"""Unit tests for tree_service.build_tree() — pure Python, no DB."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.tree_service import build_tree


def _node(
    id: str,
    parent_id=None,
    name: str = "",
    level_type: str = "TESTCASE",
    sort_order: int = 0,
    project_id: str = "proj-1",
):
    return SimpleNamespace(
        id=id,
        parent_id=parent_id,
        name=name,
        level_type=level_type,
        sort_order=sort_order,
        project_id=project_id,
    )


class TestBuildTreeEmpty:
    def test_empty_list_returns_empty(self):
        assert build_tree([]) == []

    def test_no_root_nodes_returns_empty(self):
        # All nodes have a parent — none attach to root (parent_id=None)
        nodes = [_node("a", parent_id="x"), _node("b", parent_id="y")]
        assert build_tree(nodes) == []


class TestBuildTreeFlat:
    def test_single_root_node(self):
        nodes = [_node("n1", name="Root")]
        result = build_tree(nodes)
        assert len(result) == 1
        assert result[0]["id"] == "n1"
        assert result[0]["name"] == "Root"
        assert result[0]["children"] == []

    def test_two_root_nodes_sorted_by_sort_order(self):
        nodes = [_node("b", name="B", sort_order=2), _node("a", name="A", sort_order=1)]
        result = build_tree(nodes)
        assert [r["id"] for r in result] == ["a", "b"]

    def test_parent_id_none_filters_to_roots(self):
        nodes = [
            _node("root"),
            _node("child", parent_id="root"),
        ]
        result = build_tree(nodes)
        assert len(result) == 1
        assert result[0]["id"] == "root"


class TestBuildTreeNested:
    def test_one_level_children(self):
        nodes = [
            _node("root"),
            _node("child1", parent_id="root", sort_order=1),
            _node("child2", parent_id="root", sort_order=2),
        ]
        result = build_tree(nodes)
        assert len(result) == 1
        children = result[0]["children"]
        assert len(children) == 2
        assert children[0]["id"] == "child1"
        assert children[1]["id"] == "child2"

    def test_two_levels_deep(self):
        nodes = [
            _node("root"),
            _node("child", parent_id="root"),
            _node("grandchild", parent_id="child"),
        ]
        result = build_tree(nodes)
        assert result[0]["children"][0]["children"][0]["id"] == "grandchild"

    def test_children_sorted_independently_per_level(self):
        nodes = [
            _node("root"),
            _node("c2", parent_id="root", name="C2", sort_order=2),
            _node("c1", parent_id="root", name="C1", sort_order=1),
        ]
        result = build_tree(nodes)
        child_ids = [c["id"] for c in result[0]["children"]]
        assert child_ids == ["c1", "c2"]


class TestBuildTreeFields:
    def test_output_includes_required_keys(self):
        nodes = [_node("n1", project_id="proj-abc")]
        result = build_tree(nodes)
        r = result[0]
        for key in ("id", "project_id", "parent_id", "level_type", "name", "sort_order", "children"):
            assert key in r, f"missing key: {key}"

    def test_assigned_fields_default_to_none(self):
        nodes = [_node("n1")]
        result = build_tree(nodes)
        assert result[0]["assigned_to"] is None
        assert result[0]["assigned_by"] is None
        assert result[0]["assigned_at"] is None

    def test_multiple_roots_two_trees(self):
        nodes = [
            _node("root1"),
            _node("root2"),
            _node("child_of_1", parent_id="root1"),
        ]
        result = build_tree(nodes)
        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert ids == {"root1", "root2"}
        root1 = next(r for r in result if r["id"] == "root1")
        assert len(root1["children"]) == 1
