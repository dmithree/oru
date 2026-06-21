"""Runner — orchestrates adapters, dedups against the store, mutates via
the coordinator (which emits events).

Outcomes per incoming RawTask:
  - created       new task inserted
  - updated       existing task patched (fields changed)
  - closed        existing open task transitioned to done because the
                  source now reports it done
  - skipped       no-op: identical to what's already in the store
  - merged        deduped against an existing task by text hash; the
                  duplicate is not stored (we prefer the existing
                  record's source)

Run idempotently: rerunning the same migration produces "skipped" for
every record so it's safe to schedule.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .. import coordinator, parsers, store
from .base import Adapter, RawTask

logger = logging.getLogger(__name__)


@dataclass
class IngestReport:
    by_adapter: dict[str, dict[str, int]] = field(default_factory=dict)
    samples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def bump(self, adapter: str, outcome: str) -> None:
        slot = self.by_adapter.setdefault(adapter, {})
        slot[outcome] = slot.get(outcome, 0) + 1

    def sample(self, adapter: str, kind: str, payload: dict[str, Any], cap: int = 5) -> None:
        key = f"{adapter}:{kind}"
        lst = self.samples.setdefault(key, [])
        if len(lst) < cap:
            lst.append(payload)

    def totals(self) -> dict[str, int]:
        agg: dict[str, int] = {}
        for slot in self.by_adapter.values():
            for k, v in slot.items():
                agg[k] = agg.get(k, 0) + v
        return agg

    def as_dict(self) -> dict[str, Any]:
        return {
            "by_adapter": self.by_adapter,
            "totals": self.totals(),
            "samples": self.samples,
            "errors": self.errors,
        }


def _merge_enrichment(text: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """Combine parser-extracted metadata with adapter overrides.

    Overrides win for any key adapter already knows (e.g., due_at from
    a structured source). Parser fills in the rest from text tokens."""
    parsed = parsers.parse_metadata(text)
    parsed.update(overrides or {})
    return parsed


def _diff_fields(current: dict[str, Any], proposed: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields that meaningfully differ. Skips None
    proposed values so adapters never blank out existing metadata."""
    out: dict[str, Any] = {}
    for k, v in proposed.items():
        if v is None:
            continue
        if current.get(k) != v:
            out[k] = v
    return out


def ingest_raw(
    rt: RawTask,
    *,
    adapter_name: str,
    report: IngestReport,
    dry_run: bool = False,
    agent: str = "ingestor",
) -> tuple[str, Optional[dict[str, Any]]]:
    """Process one RawTask. Returns (outcome, stored_task_dict_or_None).

    Outcomes: created | updated | closed | skipped | error
    """
    enrichment = _merge_enrichment(rt.text, rt.overrides)
    existing: Optional[dict[str, Any]] = None
    via = "new"

    # 1) Try exact ext_id match within the adapter source family
    if rt.ext_id:
        ext_match = store.find_by_ext_id(rt.source, rt.ext_id)
        if ext_match:
            existing = ext_match
            via = "ext_id"

    # 2) Fallback: text-hash dedup across all sources
    if existing is None:
        th = store.canonical_text_hash(rt.text)
        candidates = store.find_by_text_hash(th)
        if candidates:
            # Prefer a candidate from the same source if available, else
            # the first match. This keeps related markdown lines from
            # absorbing Reminders tasks (and vice versa).
            same_source = [c for c in candidates if c["source"] == rt.source]
            existing = same_source[0] if same_source else candidates[0]
            via = "text_hash"

    # === Decide outcome =============================================

    if existing is None:
        # Brand-new task.
        if dry_run:
            report.bump(adapter_name, "created")
            report.sample(
                adapter_name,
                "created",
                {"text": rt.text, "source": rt.source, **enrichment, "status": rt.status},
            )
            return "created", None
        try:
            payload = {
                k: v for k, v in enrichment.items()
                if k in {
                    "due_at", "due_precision", "context_tags", "cog_type",
                    "effort_min", "priority", "recurrence",
                }
            }
            task = coordinator.create(
                rt.text,
                source=rt.source,
                status=rt.status,
                ext_id=rt.ext_id,
                project=rt.project,
                owner_agent=rt.owner_agent,
                raw=rt.raw or None,
                agent=agent,
                **payload,
            )
        except Exception as e:  # noqa: BLE001
            report.bump(adapter_name, "error")
            report.errors.append(f"{rt.source}: create failed: {e}")
            return "error", None
        report.bump(adapter_name, "created")
        return "created", task

    # We have an existing record. Decide whether to close, update or
    # skip.
    if rt.status == "done" and existing["status"] not in {"done", "dropped"}:
        if dry_run:
            report.bump(adapter_name, "closed")
            report.sample(
                adapter_name,
                "closed",
                {"id": existing["id"][:8], "text": rt.text, "source": rt.source, "via": via},
            )
            return "closed", existing
        try:
            task = coordinator.change_status(
                existing["id"], "done", agent=agent, completed_via=f"ingestor:{via}",
            )
        except Exception as e:  # noqa: BLE001
            report.bump(adapter_name, "error")
            report.errors.append(f"{rt.source}: close failed: {e}")
            return "error", existing
        report.bump(adapter_name, "closed")
        return "closed", task

    # Compute field diff (text + enrichment). We never overwrite source/
    # status/closed_at on a dedup match.
    proposed: dict[str, Any] = {}
    if rt.text and existing.get("text") != rt.text:
        # Same hash but different text spelling — refresh to whichever
        # the latest source shows. Keeps Apple Reminders edits visible.
        proposed["text"] = rt.text
    if rt.project and not existing.get("project"):
        proposed["project"] = rt.project
    if rt.owner_agent and not existing.get("owner_agent"):
        proposed["owner_agent"] = rt.owner_agent
    if rt.ext_id and not existing.get("ext_id"):
        proposed["ext_id"] = rt.ext_id

    enrichment_diff = _diff_fields(existing, enrichment)
    proposed.update(enrichment_diff)

    if not proposed:
        report.bump(adapter_name, "skipped")
        return "skipped", existing

    if dry_run:
        report.bump(adapter_name, "updated")
        report.sample(
            adapter_name,
            "updated",
            {"id": existing["id"][:8], "via": via, "diff": proposed},
        )
        return "updated", existing

    try:
        task = coordinator.update(existing["id"], agent=agent, **proposed)
    except Exception as e:  # noqa: BLE001
        report.bump(adapter_name, "error")
        report.errors.append(f"{rt.source}: update failed: {e}")
        return "error", existing
    report.bump(adapter_name, "updated")
    return "updated", task


def run_adapters(
    adapters: Iterable[Adapter],
    *,
    dry_run: bool = False,
    agent: str = "ingestor",
) -> IngestReport:
    """Run a list of adapters end-to-end. Returns the full report."""
    report = IngestReport()

    if not dry_run:
        store.init_db()

    for adapter in adapters:
        logger.info("ingestor: running adapter %s (dry_run=%s)", adapter.name, dry_run)
        try:
            for rt in adapter.read():
                ingest_raw(
                    rt,
                    adapter_name=adapter.name,
                    report=report,
                    dry_run=dry_run,
                    agent=agent,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("ingestor: adapter %s crashed", adapter.name)
            report.errors.append(f"{adapter.name}: {e}")

    return report
