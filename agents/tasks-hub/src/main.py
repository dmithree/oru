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
from typing import Any, Optional

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import coordinator, events, store
from .config import settings

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
