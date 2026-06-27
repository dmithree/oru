---
name: oru-infra
description: "Administer Oru bot infrastructure — Telegram bot settings, Docker containers, slash command registration, container health checks. Use when Дима asks about bot setup, command registration, container status, or infrastructure maintenance."
platforms: [darwin, linux]
metadata:
  hermes:
    tags: [oru, infrastructure, telegram, docker, admin]
---

# Oru Infrastructure Administration

Skill for maintaining Oru's infrastructure: the Telegram bot (@its_oru_bot), Docker containers, slash command registration, and operational health.

## When to use

- Дима asks to register, update, or remove Telegram bot commands
- Container health checks beyond simple `docker ps`
- Bot configuration changes (description, about text, menu button)
- Adding a new oru skill that needs a corresponding slash command registered in Telegram
- Any "почему команда не работает в Telegram" debugging

## Telegram Bot Command Registration

When new oru skills are added or existing ones renamed, slash commands must be synced with Telegram via the Bot API.

Bot token location: `~/.hermes/.env` → `TELEGRAM_BOT_TOKEN`

```bash
source ~/.hermes/.env
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setMyCommands" \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"command": "health", "description": "Недельный дайджест здоровья"},
      {"command": "hello", "description": "Утренний брифинг"},
      {"command": "debrief", "description": "Вечерний дебриф"},
      {"command": "travel", "description": "Статус поездки"},
      {"command": "preflight", "description": "Чеклист перед поездкой"},
      {"command": "recap", "description": "Итоги поездки"}
    ]
  }'
```

Response `{"ok":true,"result":true}` confirms success. Commands appear in Telegram autocomplete immediately.

### Other useful Bot API endpoints

- `getMyCommands` — verify currently registered commands
- `deleteMyCommands` — clear all commands
- `setMyDescription` — bot description (shown before first message)
- `setMyShortDescription` — short description (shown in profile)

All follow the same pattern: `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/<method>`.

## Docker Containers

| Container | Port | Purpose |
|-----------|------|---------|
| oru-health | 8001 | Oura + Strava health digest |
| oru-daily-briefing | 8002 | Morning brief + evening debrief |
| oru-travel-continuity | 8003 | Travel status, preflight, recap |
| oru-tasks-hub | 8004 | SQLite single-source-of-truth for ALL tasks; agents emit via POST /tasks |
| oru-self-reflection | 8005 | Self-reflection agent (depends_on tasks-hub) |

Compose location: `/Users/dmitry/Documents/GitHub/oru/`. API endpoints are bare (`/healthz`, `/tasks`, `/run`) — there is NO `/api/` prefix and NO `/` index route (both return 404). Don't probe `/` or `/api` to discover the service; hit a known endpoint.

### Health check

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep oru
```

### Restart a container

```bash
cd /Users/dmitry/Documents/GitHub/oru && docker compose restart <service-name>
```

**Service name ≠ container name.** In `docker-compose.yml` the service keys are bare (`daily-briefing`, `health`, `travel-continuity`) while `container_name:` adds the `oru-` prefix (`oru-daily-briefing`). `docker compose <cmd> <name>` needs the SERVICE name; `docker ps`/`exec`/`cp`/`restart <name>` need the CONTAINER name. Mixing them gives `no such service: oru-daily-briefing`. Check with `grep -n 'container_name\|^  [a-z].*:' docker-compose.yml`.

### Applying a code change — `restart` does NOT do it

**CRITICAL: `docker compose restart <svc>` re-runs the OLD baked image. It NEVER applies a code edit.** Agents do NOT mount `src/` as a volume — code is `COPY`'d into the image (see each Dockerfile, e.g. `COPY src/ ./src/`). The compose volumes are only `./state` and read-only data dirs. So after editing `agents/*/src/*.py`, a plain restart silently keeps running stale code — you'll edit the file, restart, re-test, and see the bug persist with no error. Confirm what's actually running with:

```bash
docker exec oru-tasks-hub sh -c 'sed -n "14,40p" /opt/tasks-hub/src/coordinator.py'  # workdir is /opt/<svc>
```

Two correct ways to apply a Python source change:

**(A) Full rebuild (durable, survives recreate):**
```bash
cd /Users/dmitry/Documents/GitHub/oru
docker compose build <service>            # SERVICE name, no oru- prefix
docker compose up -d --no-build <service> # recreate container from fresh image
sleep 4 && curl -sS -m 5 http://localhost:<port>/healthz
```
Note: `docker compose up -d <svc>` may trip Hermes' "long-lived server" guard — run it via `background=true` + `process(action=wait)`, or use `--no-build` after a separate `build`.

**(B) Fast hot-copy (seconds, but LOST on next rebuild/recreate):**
```bash
docker cp /Users/dmitry/Documents/GitHub/oru/agents/<svc>/src/main.py \
  oru-<svc>:/opt/<svc>/src/main.py
docker restart oru-<svc>
sleep 4 && curl -sS -m 5 http://localhost:<port>/healthz
```

Always edit the file in the REPO too (not just the container copy) so the change survives the next real rebuild. The `python:3.11-slim` base-image rebuild can hang for minutes on slow docker.io network — `docker pull python:3.11-slim` writes progress via `\r` so a redirected log looks empty mid-pull; poll `docker images python:3.11-slim` rather than assuming it's stuck.

## tasks-hub: task store & dedup

oru-tasks-hub (8004) is the SQLite SoT. Other agents (health, self-reflection) emit tasks via `POST /tasks` using a vendored `tasks_hub_client.py` / `agent_emitter.py`. Recurring follow-ups use `upsert_recurring(text, ext_id=..., recurrence=...)` which is meant to be idempotent.

**Idempotency lives in `coordinator.create()` on the SERVER, not the client.** The client always POSTs; the server's `create()` checks `store.find_by_ext_id(source, ext_id)` and returns the existing row instead of inserting a duplicate. If you see duplicate tasks with the SAME `ext_id` accumulating (classic: health digest emits a fresh "Сдать анализы крови" every run), the dedup branch is missing from the RUNNING IMAGE even if it's present in the repo source — see the `restart`-doesn't-apply-code trap above. Fix = rebuild tasks-hub, not patch the emitter.

Inspect / clean duplicates via the HTTP API (the DB path inside the container is `/opt/state/tasks.db`; `sqlite3` CLI is not installed in the image, so prefer the API):

```bash
# list an agent's tasks
curl -s "http://localhost:8004/tasks?owner_agent=health" | jq '{count, tasks:[.tasks[]|{id,text,ext_id}]}'
# delete a specific duplicate (hard delete, emits tombstone)
curl -s -X DELETE "http://localhost:8004/tasks/<id>" | jq .
# verify dedup after rebuild: run the emitter twice, count must stay 1
curl -s -X POST "http://localhost:8001/run?notify=false" >/dev/null
docker logs oru-tasks-hub 2>&1 | grep upsert | tail   # expect "already exists ... skipping create"
```

Note the response shape: `GET /tasks` returns `{"tasks":[...],"count":N}` (object), not a bare array — `jq '.tasks[]'`, not `jq '.[]'`.

## Pitfalls

- **`docker compose restart` does NOT apply code edits** — it re-runs the baked image. Rebuild (`build` + `up -d --no-build`) or hot-copy with `docker cp`. This silently wastes iterations: you edit, restart, re-test, and the bug persists with no error. See "Applying a code change" above.
- A "successful" log line from the EMITTING agent (e.g. `health: ensured recurring task ...`) does NOT prove the server-side action worked — the client logs success regardless of whether tasks-hub inserted or skipped. Verify on the tasks-hub side (count + `grep upsert` in its logs), not the emitter's.
- `setMyCommands` replaces the ENTIRE command list — always send the full set, not incremental additions
- Bot token in `.env` is masked in search results (`***`); use `source` to load it, don't try to read the raw value
- When adding a new oru skill with a slash command, update BOTH: (1) the skill in ~/.hermes/skills/oru/ and (2) the Telegram command list via this procedure
