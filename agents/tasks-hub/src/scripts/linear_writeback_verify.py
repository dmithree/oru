"""Dry-run verification of Linear write-back without actually closing
an issue.

Closing real Linear issues from a test is risky (issue history shows
"completed by integration"). This script instead:

  1. Picks the first linear-sourced task in store
  2. Resolves the team's "completed" workflow state ID via Linear
     GraphQL (read-only query)
  3. Prints the exact issueUpdate mutation payload that close_issue
     WOULD execute
  4. Optionally with --execute, runs the mutation for real

Usage (inside container):

    docker compose exec tasks-hub python -m src.scripts.linear_writeback_verify
    docker compose exec tasks-hub python -m src.scripts.linear_writeback_verify --execute --task-id <id>
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .. import linear_writeback as lw
from .. import store


def pick_linear_task(task_id: str | None) -> dict[str, Any] | None:
    if task_id:
        t = store.get_task(task_id)
        if t and (t.get("source") or "").startswith("linear:"):
            return t
        return None
    candidates = store.list_tasks(source_prefix="linear:", limit=50)
    for t in candidates:
        if (t.get("raw") or {}).get("id"):
            return t
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Linear write-back verifier")
    parser.add_argument("--task-id", help="specific store task id (default: first linear task with UUID)")
    parser.add_argument(
        "--as-kind", default="completed",
        choices=("completed", "canceled"),
        help="target Linear workflow state type",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="actually call issueUpdate (closes the issue in Linear!)",
    )
    args = parser.parse_args(argv)

    task = pick_linear_task(args.task_id)
    if not task:
        print("No linear-sourced task with raw.id found in store. Re-run /ingest with linear adapter first.")
        return 1

    raw = task.get("raw") or {}
    print(f"Task picked: store_id={task['id'][:8]}  identifier={task.get('ext_id')}  text={task['text']!r}")
    print(f"  source={task['source']}  status={task['status']}")
    print(f"  linear_uuid={raw.get('id')}  linear_state={raw.get('state')}")
    print()

    team_key = lw._team_key_from_identifier(task.get("ext_id") or "")
    if not team_key:
        print("ERROR: could not extract team key from identifier")
        return 2
    print(f"Team key: {team_key}")
    print(f"Target state type: {args.as_kind}")

    try:
        state_id = lw._resolve_state(team_key, args.as_kind)
    except RuntimeError as e:
        print(f"ERROR resolving workflow state: {e}")
        return 3
    if not state_id:
        print(f"ERROR: no {args.as_kind} state found for team {team_key}")
        return 4
    print(f"Resolved state_id: {state_id}")
    print()

    print("Mutation that WOULD execute:")
    print(json.dumps({
        "query": "mutation IssueUpdate($id: String!, $stateId: String!) { issueUpdate(id: $id, input: { stateId: $stateId }) { success issue { identifier state { name type } } } }",
        "variables": {"id": raw["id"], "stateId": state_id},
    }, ensure_ascii=False, indent=2))

    if not args.execute:
        print()
        print("(dry-run; pass --execute to actually close in Linear)")
        return 0

    print()
    print("EXECUTING for real...")
    result = lw.close_issue(task, as_kind=args.as_kind)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 5


if __name__ == "__main__":
    sys.exit(main())
