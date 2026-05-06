"""Tests for jira_graph_walker.py — pure logic functions only.

These tests do not make JIRA API calls. They test the decision-making
functions that determine traversal behavior and data extraction.
"""

import pytest


def test_is_strat_ticket_by_project_prefix():
    from jira_graph_walker import is_strat_ticket

    issue = {"issue_key": "RHAISTRAT-1077", "issue_type": "Outcome"}
    assert is_strat_ticket(issue) is True


def test_is_strat_ticket_by_issue_type_outcome():
    from jira_graph_walker import is_strat_ticket

    issue = {"issue_key": "PROJ-100", "issue_type": "Outcome"}
    assert is_strat_ticket(issue) is True


def test_is_strat_ticket_false_for_story():
    from jira_graph_walker import is_strat_ticket

    issue = {"issue_key": "INFERENG-6188", "issue_type": "Story"}
    assert is_strat_ticket(issue) is False


def test_is_strat_ticket_false_for_epic():
    from jira_graph_walker import is_strat_ticket

    issue = {"issue_key": "INFERENG-5000", "issue_type": "Epic"}
    assert is_strat_ticket(issue) is False


def test_is_strat_ticket_feature_request():
    from jira_graph_walker import is_strat_ticket

    issue = {"issue_key": "RHAIRFE-1056", "issue_type": "Feature Request"}
    assert is_strat_ticket(issue) is True


def test_build_ticket_entry_central():
    from jira_graph_walker import build_ticket_entry

    issue_data = {
        "issue_key": "INFERENG-6188",
        "summary": "Add vLLM support",
        "description": "Detailed description here",
        "issue_type": "Story",
        "issue_category": "Story",
        "status": "In Progress",
        "priority": "Critical",
        "git_links": ["https://github.com/org/repo/pull/42"],
        "custom_fields": {"fix_versions": ["3.4"]},
        "comments": [],
        "url": "https://redhat.atlassian.net/browse/INFERENG-6188",
    }
    graph_data = {
        "web_links": {"total": 0, "links": []},
        "auto_discovered_urls": {"pull_requests": [], "google_docs": []},
    }
    entry = build_ticket_entry(issue_data, graph_data, "central", 0, None)

    assert entry["key"] == "INFERENG-6188"
    assert entry["relationship"] == "central"
    assert entry["distance"] == 0
    assert entry["parent_key"] is None
    assert entry["git_links"] == ["https://github.com/org/repo/pull/42"]
    assert entry["summary"] == "Add vLLM support"


def test_build_ticket_entry_child():
    from jira_graph_walker import build_ticket_entry

    issue_data = {
        "issue_key": "INFERENG-6201",
        "summary": "Child task",
        "description": "",
        "issue_type": "Task",
        "issue_category": "Task",
        "status": "To Do",
        "priority": "Major",
        "git_links": [],
        "custom_fields": {},
        "comments": [],
        "url": "https://redhat.atlassian.net/browse/INFERENG-6201",
    }
    entry = build_ticket_entry(issue_data, None, "child", 2, "INFERENG-6188")

    assert entry["relationship"] == "child"
    assert entry["distance"] == 2
    assert entry["parent_key"] == "INFERENG-6188"
    assert entry["web_links"] == {"total": 0, "links": []}
    assert entry["auto_discovered_urls"] == {"pull_requests": [], "google_docs": []}


from unittest.mock import MagicMock


def _make_issue(key, issue_type="Story", summary="Test"):
    return {
        "issue_key": key,
        "issue_type": issue_type,
        "issue_category": issue_type,
        "summary": summary,
        "description": "",
        "status": "In Progress",
        "priority": "Major",
        "git_links": [],
        "custom_fields": {},
        "comments": [],
        "url": f"https://redhat.atlassian.net/browse/{key}",
    }


def test_walk_up_finds_strat():
    from jira_graph_walker import walk_up_to_strat

    issues = {
        "INFERENG-5000": _make_issue("INFERENG-5000", "Epic", "Parent Epic"),
        "RHAISTRAT-1077": _make_issue("RHAISTRAT-1077", "Outcome", "Unified RHAII"),
    }
    reader = MagicMock()
    reader.get_issue_data.side_effect = lambda key: issues[key]

    central_issue = _make_issue("INFERENG-6188")
    central_graph = {
        "parent": {"key": "INFERENG-5000"},
        "ancestors": [
            {"key": "INFERENG-5000"},
            {"key": "RHAISTRAT-1077"},
        ],
    }

    strat, ancestors, errors = walk_up_to_strat(reader, central_issue, central_graph)

    assert strat is not None
    assert strat["key"] == "RHAISTRAT-1077"
    assert "RHAISTRAT-1077" in ancestors
    assert ancestors["RHAISTRAT-1077"]["relationship"] == "ancestor_strat"
    assert "INFERENG-5000" in ancestors
    assert ancestors["INFERENG-5000"]["relationship"] == "ancestor"
    assert errors == []


def test_walk_up_central_is_strat():
    from jira_graph_walker import walk_up_to_strat

    reader = MagicMock()
    central_issue = _make_issue("RHAISTRAT-1077", "Outcome", "Unified RHAII")
    central_graph = {"parent": None, "ancestors": []}

    strat, ancestors, errors = walk_up_to_strat(reader, central_issue, central_graph)

    assert strat["key"] == "RHAISTRAT-1077"
    assert ancestors == {}
    reader.get_issue_data.assert_not_called()


def test_walk_up_no_strat_found():
    from jira_graph_walker import walk_up_to_strat

    reader = MagicMock()
    reader.get_issue_data.return_value = _make_issue("PROJ-1", "Epic", "Some Epic")
    reader.get_ticket_graph.return_value = {"parent": None, "ancestors": []}

    central_issue = _make_issue("INFERENG-6188")
    central_graph = {
        "parent": {"key": "PROJ-1"},
        "ancestors": [{"key": "PROJ-1"}],
    }

    strat, ancestors, errors = walk_up_to_strat(reader, central_issue, central_graph)

    assert strat is None
    assert "PROJ-1" in ancestors
