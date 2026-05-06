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


def walk_up_to_strat(reader, central_issue_data, central_graph_data):
    """Walk parent links from central ticket up to the STRAT.

    Returns:
        Tuple of (strat_info, ancestors_dict, errors) where:
        - strat_info: {"key": ..., "summary": ..., "issue_type": ...} or None
        - ancestors_dict: {key: ticket_entry} for all ancestors visited
        - errors: list of error strings
    """
    ancestors = {}
    errors = []
    visited = {central_issue_data["issue_key"]}

    if is_strat_ticket(central_issue_data):
        strat_info = {
            "key": central_issue_data["issue_key"],
            "summary": central_issue_data.get("summary", ""),
            "issue_type": central_issue_data.get("issue_type", ""),
        }
        return strat_info, ancestors, errors

    parent_key = central_graph_data.get("parent")
    if isinstance(parent_key, dict):
        parent_key = parent_key.get("key")

    ancestor_list = central_graph_data.get("ancestors", [])

    chain_keys = []
    if parent_key and parent_key not in visited:
        chain_keys.append(parent_key)
    for anc in ancestor_list:
        k = anc.get("key", anc) if isinstance(anc, dict) else anc
        if k not in visited and k not in chain_keys:
            chain_keys.append(k)

    distance = 1
    strat_info = None

    for key in chain_keys:
        if key in visited:
            continue
        visited.add(key)

        try:
            issue = reader.get_issue_data(key)
        except Exception as e:
            errors.append(f"Failed to fetch ancestor {key}: {e}")
            distance += 1
            continue

        relationship = "ancestor"
        if is_strat_ticket(issue):
            relationship = "ancestor_strat"
            strat_info = {
                "key": issue["issue_key"],
                "summary": issue.get("summary", ""),
                "issue_type": issue.get("issue_type", ""),
            }

        ancestors[key] = build_ticket_entry(issue, None, relationship, distance, None)
        distance += 1

        if strat_info:
            break

    if not strat_info and chain_keys:
        last_key = chain_keys[-1] if chain_keys[-1] in visited else None
        while last_key and not strat_info:
            try:
                parent_graph = reader.get_ticket_graph(last_key)
            except Exception as e:
                errors.append(f"Failed to fetch graph for {last_key}: {e}")
                break

            next_parent = parent_graph.get("parent")
            if isinstance(next_parent, dict):
                next_parent = next_parent.get("key")

            if not next_parent or next_parent in visited:
                break

            visited.add(next_parent)
            try:
                issue = reader.get_issue_data(next_parent)
            except Exception as e:
                errors.append(f"Failed to fetch ancestor {next_parent}: {e}")
                break

            relationship = "ancestor"
            if is_strat_ticket(issue):
                relationship = "ancestor_strat"
                strat_info = {
                    "key": issue["issue_key"],
                    "summary": issue.get("summary", ""),
                    "issue_type": issue.get("issue_type", ""),
                }

            ancestors[next_parent] = build_ticket_entry(
                issue, None, relationship, distance, None
            )
            distance += 1
            last_key = next_parent

    return strat_info, ancestors, errors


def walk_down_from(reader, start_key, visited, start_distance=1):
    """Walk down from a ticket through all children (breadth-first).

    Args:
        reader: JiraReader instance
        start_key: Ticket key to start walking down from
        visited: Set of already-visited ticket keys (mutated in place)
        start_distance: Distance value for direct children of start_key

    Returns:
        Tuple of (children_dict, errors) where:
        - children_dict: {key: ticket_entry} for all descendants
        - errors: list of error strings
    """
    children = {}
    errors = []
    queue = deque([(start_key, start_distance)])

    while queue:
        parent_key, distance = queue.popleft()

        try:
            graph = reader.get_ticket_graph(parent_key)
        except Exception as e:
            errors.append(f"Failed to fetch children of {parent_key}: {e}")
            continue

        child_issues = graph.get("children", {}).get("issues", [])

        for child in child_issues:
            child_key = child.get("key")
            if not child_key or child_key in visited:
                continue

            visited.add(child_key)

            try:
                issue = reader.get_issue_data(child_key)
            except Exception as e:
                errors.append(f"Failed to fetch child {child_key}: {e}")
                continue

            child_graph = None
            try:
                child_graph = reader.get_ticket_graph(child_key)
            except Exception:
                pass

            children[child_key] = build_ticket_entry(
                issue, child_graph, "child", distance, parent_key
            )

            queue.append((child_key, distance + 1))

    return children, errors


def walk_sideways(reader, central_key, central_graph_data, visited):
    """Walk issue links from the central ticket (one level only).

    Args:
        reader: JiraReader instance
        central_key: Central ticket key
        central_graph_data: Graph data for the central ticket
        visited: Set of already-visited ticket keys (mutated in place)

    Returns:
        Tuple of (linked_dict, errors) where:
        - linked_dict: {key: ticket_entry} for all linked tickets
        - errors: list of error strings
    """
    linked = {}
    errors = []

    links = central_graph_data.get("issue_links", {}).get("links", [])

    for link in links:
        link_key = link.get("key")
        if not link_key or link_key in visited:
            continue

        visited.add(link_key)

        try:
            issue = reader.get_issue_data(link_key)
        except Exception as e:
            errors.append(f"Failed to fetch linked ticket {link_key}: {e}")
            continue

        link_graph = None
        try:
            link_graph = reader.get_ticket_graph(link_key)
        except Exception:
            pass

        linked[link_key] = build_ticket_entry(issue, link_graph, "linked", 1, None)

    siblings = central_graph_data.get("siblings", {}).get("issues", [])

    for sib in siblings:
        sib_key = sib.get("key")
        if not sib_key or sib_key in visited:
            continue

        visited.add(sib_key)

        try:
            issue = reader.get_issue_data(sib_key)
        except Exception as e:
            errors.append(f"Failed to fetch sibling {sib_key}: {e}")
            continue

        sib_graph = None
        try:
            sib_graph = reader.get_ticket_graph(sib_key)
        except Exception:
            pass

        linked[sib_key] = build_ticket_entry(issue, sib_graph, "sibling", 1, None)

    return linked, errors
