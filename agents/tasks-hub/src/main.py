"""tasks-hub HTTP service (Phase 0).

Endpoints:
  GET  /healthz                — readiness probe
  GET  /stats                  — counts by status / source + schema version
  GET  /events?limit=&kind=    — tail event log
  POST /tasks                  — create task (body: full payload)
  GET  /tasks                  — list (query: status, source, owner_agent, due_before, context_tag, limit, offset)
  GET  /tasks/{id}             — single task
  PATCH /tasks/{id}            — update mutable fields (NOT status)
  POST /tasks/{id}/status      — explicit transition (body: {to, completed_via?, defer_until?, blocked_by?, waiting_on?, reason?})
  POST /tasks/{id}/triage      — inbox->open|dropped (body: {decision})
  DELETE /tasks/{id}           — hard delete (tombstone event)

No ingestion / render / Telegram yet — those land in Phase 1+.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import backpressure as bp, coordinator, debrief as debrief_module, events, reminders_commands, store
from .config import settings
from .ingestor import runner as ingest_runner
from .ingestor.linear_adapter import LinearAdapter
from .ingestor.markdown_adapter import MarkdownAdapter, detect_repo_root
from .ingestor.reminders_adapter import RemindersAdapter
from .render import renderer as view_renderer
from .render import view as view_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("tasks-hub")

app = FastAPI(title="tasks-hub", version="0.1.0")


# === Pydantic request models =========================================

class TaskCreate(BaseModel):
    text: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)
    status: str = "open"
    ext_id: Optional[str] = None
    priority: Optional[str] = None
    due_at: Optional[str] = None
    due_precision: Optional[str] = None
    context_tags: Optional[list[str]] = None
    cog_type: Optional[str] = None
    effort_min: Optional[int] = None
    energy: Optional[str] = None
    recurrence: Optional[str] = None
    project: Optional[str] = None
    owner_agent: Optional[str] = None
    blocked_by: Optional[str] = None
    waiting_on: Optional[str] = None
    defer_until: Optional[str] = None
    parent_id: Optional[str] = None
    raw: Optional[dict[str, Any]] = None
    agent: str = "tasks-hub"


class TaskUpdate(BaseModel):
    text: Optional[str] = None
    source: Optional[str] = None
    ext_id: Optional[str] = None
    priority: Optional[str] = None
    due_at: Optional[str] = None
    due_precision: Optional[str] = None
    context_tags: Optional[list[str]] = None
    cog_type: Optional[str] = None
    effort_min: Optional[int] = None
    energy: Optional[str] = None
    recurrence: Optional[str] = None
    project: Optional[str] = None
    owner_agent: Optional[str] = None
    blocked_by: Optional[str] = None
    waiting_on: Optional[str] = None
    defer_until: Optional[str] = None
    parent_id: Optional[str] = None
    raw: Optional[dict[str, Any]] = None
    agent: str = "tasks-hub"


class StatusChange(BaseModel):
    to: str
    completed_via: Optional[str] = None
    defer_until: Optional[str] = None
    blocked_by: Optional[str] = None
    waiting_on: Optional[str] = None
    reason: Optional[str] = None
    agent: str = "tasks-hub"


class TriageDecision(BaseModel):
    decision: str
    agent: str = "user"


class BulkTriageItem(BaseModel):
    id: str
    decision: str   # "open" | "dropped"


class BulkTriage(BaseModel):
    items: list[BulkTriageItem]
    agent: str = "user"


# === Endpoints =======================================================

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "agent": "tasks-hub", "schema_version": store.SCHEMA_VERSION}


@app.get("/stats")
def get_stats() -> dict:
    return {
        "store": store.stats(),
        "events_count": events.count(),
    }


@app.get("/events")
def get_events(
    limit: int = Query(50, ge=1, le=1000),
    kind: Optional[str] = Query(None),
) -> dict:
    return {"events": events.tail(limit=limit, kind=kind)}


@app.post("/tasks", status_code=201)
def create_task(body: TaskCreate) -> dict:
    try:
        fields = body.model_dump(exclude={"text", "source", "status", "agent"}, exclude_none=True)
        task = coordinator.create(
            body.text,
            source=body.source,
            status=body.status,
            agent=body.agent,
            **fields,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"task": task}


@app.get("/tasks")
def list_tasks(
    status: Optional[list[str]] = Query(None),
    source_prefix: Optional[str] = Query(None),
    owner_agent: Optional[str] = Query(None),
    due_before: Optional[str] = Query(None),
    context_tag: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    order: str = Query("due_at_then_created"),
) -> dict:
    tasks = store.list_tasks(
        status=status,
        source_prefix=source_prefix,
        owner_agent=owner_agent,
        due_before=due_before,
        context_tag=context_tag,
        limit=limit,
        offset=offset,
        order=order,
    )
    return {"tasks": tasks, "count": len(tasks)}


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task": task}


@app.patch("/tasks/{task_id}")
def patch_task(task_id: str, body: TaskUpdate) -> dict:
    fields = body.model_dump(exclude={"agent"}, exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        task = coordinator.update(task_id, agent=body.agent, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"task": task}


@app.post("/tasks/{task_id}/status")
def change_status(task_id: str, body: StatusChange) -> dict:
    try:
        task = coordinator.change_status(
            task_id,
            body.to,
            agent=body.agent,
            completed_via=body.completed_via,
            defer_until=body.defer_until,
            blocked_by=body.blocked_by,
            waiting_on=body.waiting_on,
            reason=body.reason,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found")
    except store.InvalidTransition as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"task": task}


@app.post("/tasks/{task_id}/triage")
def triage(task_id: str, body: TriageDecision) -> dict:
    try:
        task = coordinator.triage(task_id, body.decision, agent=body.agent)
    except KeyError:
        raise HTTPException(status_code=404, detail="task not found")
    except store.InvalidTransition as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"task": task}


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str, reason: Optional[str] = Query(None)) -> dict:
    deleted = coordinator.delete(task_id, reason=reason)
    if not deleted:
        raise HTTPException(status_code=404, detail="task not found")
    return {"deleted": True}


# === Ingestion =======================================================


class IngestRequest(BaseModel):
    sources: list[str] = Field(default_factory=lambda: ["markdown", "reminders", "linear"])
    dry_run: bool = False
    agent: str = "ingestor"
    personal_agent_root: Optional[str] = None
    reminders_file: Optional[str] = None


def _build_adapters(req: IngestRequest) -> list:
    adapters: list = []
    if "markdown" in req.sources:
        root = Path(req.personal_agent_root) if req.personal_agent_root else detect_repo_root()
        adapters.append(MarkdownAdapter(repo_root=root))
    if "reminders" in req.sources:
        rem_path = Path(req.reminders_file or "/opt/state/reminders.json")
        adapters.append(RemindersAdapter(file_path=rem_path))
    if "linear" in req.sources:
        adapters.append(LinearAdapter())
    return adapters


# === Backpressure (idea 18) ==========================================


@app.get("/stale")
def stale(
    older_than_days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=200),
) -> dict:
    tasks = bp.find_stale(older_than_days=older_than_days, limit=limit)
    return {
        "tasks": tasks,
        "count": len(tasks),
        "summary": bp.summary(older_than_days=older_than_days),
    }


class StaleDecision(BaseModel):
    id: str
    action: str   # "keep" | "drop" | "defer"
    defer_until: Optional[str] = None


class StaleTriage(BaseModel):
    items: list[StaleDecision]
    agent: str = "backpressure"


@app.post("/stale/triage")
def stale_triage(body: StaleTriage) -> dict:
    results: list[dict[str, Any]] = []
    for item in body.items:
        row: dict[str, Any] = {"id": item.id, "action": item.action}
        try:
            if item.action == "keep":
                # Touch the task so it falls out of the stale window
                task = coordinator.update(item.id, agent=body.agent, raw=None)
                row["ok"] = True
                row["status"] = task["status"]
            elif item.action == "drop":
                task = coordinator.change_status(item.id, "dropped",
                                                 agent=body.agent, reason="stale_cleanup")
                row["ok"] = True
            elif item.action == "defer":
                if not item.defer_until:
                    raise ValueError("defer requires defer_until")
                task = coordinator.change_status(item.id, "deferred",
                                                 agent=body.agent,
                                                 defer_until=item.defer_until,
                                                 reason="stale_cleanup")
                row["ok"] = True
            else:
                raise ValueError(f"unknown action: {item.action!r}")
        except KeyError:
            row["ok"] = False
            row["error"] = "not_found"
        except (ValueError, store.InvalidTransition) as e:
            row["ok"] = False
            row["error"] = str(e)
        results.append(row)
    ok = sum(1 for r in results if r["ok"])
    return {"results": results, "summary": {"ok": ok, "failed": len(results) - ok}}


# === Debrief =========================================================


class DebriefIngestRequest(BaseModel):
    user_text: str = Field(..., min_length=1)
    model: Optional[str] = None
    agent: str = "debrief"


@app.post("/debrief/ingest")
async def debrief_ingest(body: DebriefIngestRequest) -> dict:
    """Ingest a freeform end-of-day debrief: LLM matches statements to
    candidate tasks, this endpoint applies the resulting events
    (completed/deferred/blocked/waiting/created) through the
    coordinator and writes a human-readable file to
    state/debriefs/YYYY-MM-DD-debrief.md."""
    if not (settings.anthropic_api_key or
            (Path(".env").exists() and "ANTHROPIC_API_KEY" in Path(".env").read_text())):
        # Best-effort guard; the actual check happens inside call_llm.
        pass

    def _run() -> dict[str, Any]:
        return debrief_module.ingest_debrief(body.user_text, model=body.model, agent=body.agent)

    try:
        result = await asyncio.to_thread(_run)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, **result}


# === Reminders bridge ================================================


@app.get("/reminders/queue")
def reminders_queue_status() -> dict:
    return {
        "pending": reminders_commands.pending_count(),
        "recent_results": reminders_commands.read_results(limit=10),
    }


class ReminderCreate(BaseModel):
    name: str
    list_name: str = "AI"
    due: Optional[str] = None
    body: Optional[str] = None
    task_id: Optional[str] = None


@app.post("/reminders/queue/create")
def reminders_queue_create(body: ReminderCreate) -> dict:
    cmd = reminders_commands.enqueue_create(
        body.name,
        list_name=body.list_name,
        due=body.due,
        body=body.body,
        task_id=body.task_id,
    )
    return {"ok": True, "cmd": cmd}


# === Inbox triage ====================================================


@app.get("/inbox")
def inbox(limit: int = Query(100, ge=1, le=500)) -> dict:
    """List all tasks awaiting triage (status='inbox'). Newest first."""
    tasks = store.list_tasks(status=["inbox"], limit=limit, order="updated")
    return {"tasks": tasks, "count": len(tasks)}


@app.post("/inbox/triage")
def inbox_triage(body: BulkTriage) -> dict:
    """Apply triage decisions to multiple inbox tasks at once.

    Body: {items: [{id, decision: "open"|"dropped"}], agent}.
    Returns per-item outcomes so the caller can highlight failures."""
    results: list[dict[str, Any]] = []
    for item in body.items:
        try:
            task = coordinator.triage(item.id, item.decision, agent=body.agent)
            results.append({
                "id": item.id,
                "decision": item.decision,
                "ok": True,
                "new_status": task["status"],
            })
        except KeyError:
            results.append({"id": item.id, "decision": item.decision, "ok": False, "error": "not_found"})
        except store.InvalidTransition as e:
            results.append({"id": item.id, "decision": item.decision, "ok": False, "error": str(e)})
        except ValueError as e:
            results.append({"id": item.id, "decision": item.decision, "ok": False, "error": str(e)})
    ok_count = sum(1 for r in results if r["ok"])
    return {"results": results, "summary": {"ok": ok_count, "failed": len(results) - ok_count}}


# === Render ==========================================================


_DEFAULT_TEMPLATE = {
    "morning": "morning.j2",
    "evening": "evening.j2",
    "living":  "living.j2",
}


@app.get("/render/{view_name}")
async def render_view(
    view_name: str,
    format: str = Query("both", pattern="^(json|markdown|both)$"),
    template: Optional[str] = Query(None, description="override default template"),
) -> dict:
    """Run a view spec against the store and optionally render markdown.

    `view_name` resolves to src/render/views/<name>.yaml. Default
    template lookup: morning -> morning.j2, evening -> evening.j2,
    otherwise <view_name>.j2."""
    try:
        result = await asyncio.to_thread(view_module.run_view, view_name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"view not found: {view_name}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    out: dict[str, Any] = {"ok": True, "view": result}
    if format in ("markdown", "both"):
        tpl = template or _DEFAULT_TEMPLATE.get(view_name, f"{view_name}.j2")
        try:
            md = await asyncio.to_thread(view_renderer.render, result, template=tpl)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"render failed: {e}")
        out["markdown"] = md
    if format == "json":
        out.pop("markdown", None)
    return out


# === Ingest ==========================================================


@app.post("/ingest")
async def ingest(body: IngestRequest) -> dict:
    """Run the universal ingestor across the requested sources.

    Same code path the migration script uses — exposed over HTTP so
    other agents (or a scheduler) can trigger refresh without exec'ing
    into the container. `dry_run=true` reports without mutating.
    """
    adapters = _build_adapters(body)
    if not adapters:
        raise HTTPException(status_code=400, detail="no valid sources in request")

    def _run() -> dict:
        report = ingest_runner.run_adapters(adapters, dry_run=body.dry_run, agent=body.agent)
        return report.as_dict()

    report_dict = await asyncio.to_thread(_run)
    return {"ok": True, "dry_run": body.dry_run, "report": report_dict}


# === Entry ===========================================================

async def main() -> None:
    store.init_db()
    logger.info("tasks-hub starting (port %d, db=%s, events=%s)",
                settings.http_port, settings.db_file, settings.events_file)
    config = uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_config=None)
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
