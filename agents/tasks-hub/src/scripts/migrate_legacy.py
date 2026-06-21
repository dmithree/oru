"""One-shot legacy migration (idea 19).

Reads the 6 personal-agent markdown task files, the host Reminders
snapshot, and the (stubbed) Linear source — feeds them through the
universal ingestor with dedup.

Usage (inside container):

    docker compose exec tasks-hub python -m src.scripts.migrate_legacy --dry-run
    docker compose exec tasks-hub python -m src.scripts.migrate_legacy --apply

Usage (host venv, smoke):

    DB_FILE=/tmp/tasks-hub-smoke/tasks.db \\
      EVENTS_FILE=/tmp/tasks-hub-smoke/events.jsonl \\
      .venv/bin/python -m src.scripts.migrate_legacy --dry-run

The script writes a Markdown report to state/migration-YYYY-MM-DD.md
on apply runs (and prints the same report on dry-runs).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import store
from ..config import settings
from ..ingestor import runner
from ..ingestor.linear_adapter import LinearAdapter
from ..ingestor.markdown_adapter import MarkdownAdapter, detect_repo_root
from ..ingestor.reminders_adapter import RemindersAdapter

logger = logging.getLogger("migrate_legacy")


def build_adapters(
    *, personal_agent_root: Path, reminders_file: Path,
) -> list[Any]:
    return [
        MarkdownAdapter(repo_root=personal_agent_root),
        RemindersAdapter(file_path=reminders_file),
        LinearAdapter(),
    ]


def render_report(report_dict: dict[str, Any], *, dry_run: bool) -> str:
    lines: list[str] = []
    title = "DRY-RUN" if dry_run else "APPLIED"
    lines.append(f"# tasks-hub migration — {title}")
    lines.append("")
    lines.append(f"- generated_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append(f"- db_file: {settings.db_file}")
    lines.append(f"- events_file: {settings.events_file}")
    lines.append("")
    lines.append("## Totals")
    totals = report_dict.get("totals", {})
    for k in ("created", "updated", "closed", "skipped", "error"):
        if k in totals:
            lines.append(f"- {k}: {totals[k]}")
    lines.append("")
    lines.append("## By adapter")
    for adapter, slot in (report_dict.get("by_adapter") or {}).items():
        parts = ", ".join(f"{k}={v}" for k, v in sorted(slot.items()))
        lines.append(f"- {adapter}: {parts}")
    lines.append("")
    samples = report_dict.get("samples") or {}
    if samples:
        lines.append("## Samples (first 5 per outcome)")
        for key in sorted(samples):
            lines.append(f"### {key}")
            for s in samples[key]:
                lines.append(f"- {json.dumps(s, ensure_ascii=False)}")
            lines.append("")
    errors = report_dict.get("errors") or []
    if errors:
        lines.append("## Errors")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy task sources into tasks.db")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dry-run", action="store_true", help="report without writes")
    grp.add_argument("--apply", action="store_true", help="actually mutate the store")
    parser.add_argument(
        "--reminders-file",
        default="/opt/state/reminders.json",
        help="snapshot path (host: /Users/dmitry/Documents/GitHub/oru/state/reminders.json)",
    )
    parser.add_argument(
        "--personal-agent-root",
        default=None,
        help="path to personal-agent repo (auto-detect if omitted)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    root = Path(args.personal_agent_root) if args.personal_agent_root else detect_repo_root()
    rem_path = Path(args.reminders_file)
    if not rem_path.exists() and rem_path == Path("/opt/state/reminders.json"):
        host_alt = Path("/Users/dmitry/Documents/GitHub/oru/state/reminders.json")
        if host_alt.exists():
            rem_path = host_alt
            logger.info("reminders: container path missing, falling back to host %s", rem_path)

    logger.info("migrate: personal_agent_root=%s", root)
    logger.info("migrate: reminders_file=%s", rem_path)
    logger.info("migrate: dry_run=%s", args.dry_run)

    store.init_db()

    adapters = build_adapters(personal_agent_root=root, reminders_file=rem_path)
    report = runner.run_adapters(adapters, dry_run=args.dry_run, agent="migration")
    report_dict = report.as_dict()

    if args.json:
        print(json.dumps(report_dict, ensure_ascii=False, indent=2))
    else:
        print(render_report(report_dict, dry_run=args.dry_run))

    # Persist a copy on apply so the cutover is auditable.
    if args.apply:
        out_dir = Path(settings.db_file).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).date().isoformat()
        report_file = out_dir / f"migration-{stamp}.md"
        report_file.write_text(render_report(report_dict, dry_run=False), encoding="utf-8")
        logger.info("migration report written to %s", report_file)

    if report_dict.get("errors"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
