# tasks-hub

Unified task system for the Oru Hermes stack. Single source of truth for
everything Дима's tracking — markdown notes, Apple Reminders, Linear
issues, voice thoughts, agent-emitted follow-ups — projected into one
SQLite store with rich metadata and rendered as morning/evening/living
views for the brief and Telegram skills.

Implements the 20-idea plan at
`~/.claude/plans/tender-spinning-engelbart.md` (Phases 0–5 + 1.5, 1.6,
2.5, 4.5, 5.5, and Portion 1–3 follow-ups).

## At a glance

```
                ┌──── markdown_adapter   (everything/personal/**, pfm/**)
                ├──── reminders_adapter  (state/reminders.json, host bridge)
                ├──── linear_adapter     (Linear GraphQL, assigned issues)
                ├──── thoughts_adapter   (state/thoughts-queue.jsonl)
sources ───────►├──── agent_emitter      (health, gtv, self-reflection ...)
                │                       │
                │            ┌──────────┴───────────┐
                │            ▼                      ▼
                │      universal ingestor    event log (append-only)
                │            │                      │
                │            └───────►  tasks.db (SQLite) ◄──── coordinator
                │                            │                    ▲
                │              ┌─────────────┼────────────┐       │
                │              ▼             ▼            ▼       │
                │         brief view    debrief view   /tasks    HTTP
                │         (morning)     (evening)      living    POST/PATCH
                │              │             │            │       │
                │              ▼             ▼            ▼       │
                │          Telegram      Telegram      Hermes ────┘
                │             via daily-briefing + skills
```

## Modules

| File | Role |
|---|---|
| `src/store.py` | SQLite schema + state machine (9 statuses, validated transitions) + dedup by canonical text hash + idempotent migrations via `PRAGMA user_version`. |
| `src/events.py` | Append-only JSONL event log with fcntl locks. The log is the durable record; SQLite is the materialization. |
| `src/coordinator.py` | Thin wrapper: every store mutation emits a matching event. Auto-enriches text on create (strips `@tags`, `~effort`, `cog:type`, `!P`, `— due`, `every:`). Side-effects: recurrence respawn on done, Reminders command queue on close, Linear write-back on close. |
| `src/parsers.py` | Pure-regex extractors for inline tokens + `clean_text()` to strip them from the display string. |
| `src/recurrence.py` | `next_due_from()` for every:Nd / every:Nw / every:Nm / every:Ny / every:weekday with leap-year and month-end clamping. |
| `src/ingestor/` | `RawTask` shape + four adapters + runner with dedup (ext_id first, text_hash fallback) and pull-direction Reminders sync. |
| `src/render/` | View executor (yaml WHERE compiler + date keyword resolver) + jinja2 wrapper + three view specs (morning / evening / living) + templates. |
| `src/debrief.py` | LLM-driven event ingestion: candidates + plan → Anthropic via litellm tool-call → `apply_events()` mutates store. Insights saved to `state/debriefs/YYYY-MM-DD-debrief.md`. |
| `src/backpressure.py` | `find_stale()` + `summary()` for the weekly Sunday cleanup. |
| `src/linear_writeback.py` | issueUpdate GraphQL mutation on done/dropped of `source=linear:*` tasks. Per-team workflow state IDs cached lazily. |
| `src/reminders_commands.py` | Container → host command queue for Apple Reminders write-back. Host watcher applies via JXA. |
| `src/agent_emitter.py` | Stdlib HTTP client for other Hermes agents to POST tasks. Vendored copy in `agents/health/src/tasks_hub_client.py`. |
| `src/telegram.py` | Minimal sendMessage helper for the scheduled backpressure prompt. |
| `src/main.py` | FastAPI + AsyncIOScheduler. Endpoints listed below. Cron jobs: reminders every 15 min, markdown+linear+thoughts every 30 min, weekly cleanup Sunday 21:00. |

## HTTP API

| Method + path | What |
|---|---|
| `GET /healthz` | Readiness probe |
| `GET /stats` | Counts by status / source + event count + schema version |
| `GET /events?limit=&kind=` | Tail event log |
| `POST /tasks` | Create with auto-enrich (parses inline tokens, cleans text) |
| `GET /tasks?status=&source_prefix=&owner_agent=&due_before=&context_tag=&limit=&offset=&order=` | List |
| `GET /tasks/{id}` / `PATCH /tasks/{id}` / `DELETE /tasks/{id}` | Single task |
| `POST /tasks/{id}/status` | Validated state transition (409 on invalid) |
| `POST /tasks/{id}/triage` | inbox → open|dropped |
| `POST /ingest` | Run universal ingestor across `sources` (markdown, reminders, linear, thoughts). `dry_run=true` for preview. |
| `GET /render/{morning|evening|living}?format=json|markdown|both` | Run view spec → optional jinja render |
| `POST /debrief/ingest` | LLM event ingestion from freeform text |
| `GET /inbox` / `POST /inbox/triage` | Bulk inbox triage |
| `GET /stale` / `POST /stale/triage` | Stale task detection + bulk keep/drop/defer |
| `GET /reminders/queue` / `POST /reminders/queue/create` | Bidirectional Reminders bridge |

## Inline token grammar

When creating a task (via `POST /tasks` or `coordinator.create`), the
text is parsed for these tokens. They're stripped from `text` and
populated as structured fields:

| Token | Field | Example |
|---|---|---|
| `@<word>` or `@<word>:<value>` | `context_tags[]` | `@phone`, `@waiting:Lyosha` |
| `~Nm` / `~Nh` / `~deep` | `effort_min` (deep=90) | `~15m`, `~2h`, `~deep` |
| `cog:<deep\|short\|ai\|admin\|social>` | `cog_type` | `cog:deep` |
| `!P0..P3` | `priority` | `!P1` |
| `— due YYYY-MM-DD` / `due:YYYY-MM` | `due_at` + `due_precision` | `— due 2026-07-15` |
| `every:Nd|w|m|y` / `every:<weekday>` | `recurrence` | `every:3m`, `every:mon` |

Example:
```
POST /tasks {"text":"Доделать onboarding @laptop ~2h !P1 cog:deep — due 2026-07-15","source":"manual"}
```
→ stored as:
```
text="Доделать onboarding"  context_tags=["@laptop"]  effort_min=120
priority="P1"  cog_type="deep"  due_at="2026-07-15"  due_precision="day"
```

## State machine

```
inbox  ──→ open ──→ next ──→ doing ──→ done
   │       │↑ ↓     │↑ ↓      │↑ ↓     │↑
   │       │  waiting │  waiting│  waiting│
   │       │↑ ↓     │↑ ↓      │↑ ↓     ↓
   │       │  blocked │  blocked│  blocked↓
   │       │↑ ↓     │↑ ↓      │↑ ↓     reopen
   │       │  deferred (defer_until)
   │       │
   └──────►dropped
```

Reopen (done→open) is allowed and clears `closed_at`/`completed_via`.
Restore (dropped→open) is allowed. Validated by `store.TRANSITIONS`.

## Scheduled jobs

Defined in `src/config.py`, started by `src/main.py`. Container timezone
is Europe/Belgrade.

| ID | Cron | What |
|---|---|---|
| `ingest-reminders` | `1,16,31,46 * * * *` | One minute after the host bridge writes its 15-min snapshot |
| `ingest-other` | `5,35 * * * *` | markdown + linear + thoughts; twice an hour |
| `cleanup-weekly` | `0 21 * * sun` | Weekly stale-task surface to Telegram |

## Host-side bridges (under `oru/host-daemons/`)

Container can't reach AppleScript or write into `~/Documents/`. Three
launchd-loaded scripts handle host concerns:

- `reminders-bridge.sh` (every 15 min): bulk-access JXA dump of all
  active Apple Reminders → `state/reminders.json`.
- `reminders-commands-watcher.sh` (every 60s): applies queued
  create/complete/snooze commands from `state/reminders-commands.jsonl`
  to Reminders.app via JXA.
- `living-markdown-sync.sh` (every hour): pulls `/render/living?format=
  markdown` and writes to a TCC-friendly path.

Install all three with:
```bash
bash oru/host-daemons/install.sh
```

Scripts live in `~/Library/Application Support/oru-host/`, plists in
`~/Library/LaunchAgents/`, state in
`~/Library/Application Support/oru-host-state/` (symlinked from
`oru/state` for Docker bind mount).

## Tests

`pytest agents/tasks-hub/tests/`. 49 tests covering:

- `test_parsers.py` — every inline token + `clean_text`
- `test_recurrence.py` — period math, weekday rollover, leap-year clamp
- `test_store.py` — state machine, transitions, dedup (incl. `@-tag` distinction), ext_id lookup, list filters, stats
- `test_view.py` — date keywords, WHERE compiler, end-to-end section execution against ephemeral SQLite, adaptive filtering signals
- `test_debrief.py` — `apply_events` for every kind (completed / deferred / blocked / created with due / unknown task_id graceful fail) without hitting the LLM

`tests/conftest.py::fresh_state` fixture monkeypatches the live
settings object with tmp_path-backed file paths so each test gets its
own sqlite + events log.

## Operational scripts

| Path | Use |
|---|---|
| `src/scripts/migrate_legacy.py` | One-shot import from personal-agent legacy markdown sources. `--dry-run` / `--apply`. |
| `src/scripts/linear_writeback_verify.py` | Verifies the Linear write-back GraphQL mutation builds correctly without firing. `--execute` to actually close. |

## Cutover history

Legacy task pipeline in `personal-agent` archived 2026-06-21 →
`scripts/_archived/2026-06-task-system-pre-hermes/` (see README there).
The 137 markdown tasks + 122 Apple Reminders + 25 Linear issues + 1
agent-emitted recurring task = 285 tasks in the live store at cutover.

Brief consumers updated in Phase 2.5: `oru/agents/daily-briefing` now
calls `tasks-hub /render/morning` instead of reading the static
`all-tasks.md`. Telegram morning brief shows structured sections
(carry-over / overdue / today / 3-3-3 / waiting / recent) verbatim.
