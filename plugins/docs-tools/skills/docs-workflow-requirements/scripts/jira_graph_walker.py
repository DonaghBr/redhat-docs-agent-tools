#!/usr/bin/env python3
"""Programmatic JIRA graph walker for requirements analysis.

Walks the JIRA hierarchy from a central ticket up to its STRAT (top-level
strategy/outcome ticket), down through all children, and sideways through
issue links. Collects ticket data and extracts repo/PR URLs.

Outputs:
    <base-path>/requirements/graph-raw.json    — all visited tickets
    <base-path>/requirements/discovered_repos.json — extracted repos/PRs

Usage:
    python3 jira_graph_walker.py --ticket INFERENG-6188 --base-path .claude/docs/infereng-6188
"""

import argparse
import json
import sys
from collections import deque
from pathlib import Path

# STRAT project prefixes — tickets in these projects are strategy-level
STRAT_PREFIXES = ("RHAISTRAT-", "RHALISTRAT-", "RHELSTRAT-", "AASSTRAT-")

# Issue types that indicate a STRAT-level ticket regardless of project
STRAT_ISSUE_TYPES = ("Outcome", "Feature Request")


def is_strat_ticket(issue_data):
    """Check if a ticket is a STRAT (top-level strategy/outcome).

    A ticket is a STRAT if its project prefix matches known STRAT projects,
    or if its issue type is Outcome or Feature Request.
    """
    key = issue_data.get("issue_key", "")
    issue_type = issue_data.get("issue_type", "")

    if any(key.startswith(prefix) for prefix in STRAT_PREFIXES):
        return True

    if issue_type in STRAT_ISSUE_TYPES:
        return True

    return False


def build_ticket_entry(issue_data, graph_data, relationship, distance, parent_key):
    """Build a standardized ticket entry for graph-raw.json.

    Args:
        issue_data: Dict from jira_reader.get_issue_data()
        graph_data: Dict from jira_reader.get_ticket_graph() (optional, can be None)
        relationship: One of "central", "ancestor", "ancestor_strat", "child", "sibling", "linked"
        distance: Integer distance from the central ticket
        parent_key: Parent ticket key (None for central/strat)
    """
    entry = {
        "key": issue_data["issue_key"],
        "summary": issue_data.get("summary", ""),
        "description": issue_data.get("description", ""),
        "issue_type": issue_data.get("issue_type", ""),
        "status": issue_data.get("status", ""),
        "priority": issue_data.get("priority", ""),
        "relationship": relationship,
        "distance": distance,
        "parent_key": parent_key,
        "git_links": issue_data.get("git_links", []),
        "web_links": {"total": 0, "links": []},
        "auto_discovered_urls": {"pull_requests": [], "google_docs": []},
        "comments": issue_data.get("comments", []),
        "custom_fields": issue_data.get("custom_fields", {}),
        "url": issue_data.get("url", ""),
    }

    if graph_data:
        entry["web_links"] = graph_data.get("web_links", {"total": 0, "links": []})
        entry["auto_discovered_urls"] = graph_data.get(
            "auto_discovered_urls", {"pull_requests": [], "google_docs": []}
        )

    return entry
